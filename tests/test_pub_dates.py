import datetime as dt
import os
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import pub_dates as P

D = dt.date


class Dates(unittest.TestCase):
    def test_publication_statements(self):
        end = D(2024, 12, 31)
        t = P.find_dates([(2, "Date of publication: 4 March 2025. Contact us at ir@example.com")], end, D(2025, 6, 1))
        self.assertEqual(t[:3], (2, D(2025, 3, 4), "day"))
        t = P.find_dates([(1, "This report was published in March 2025 and covers the year")], end, None)
        self.assertEqual(t[:3], (2, D(2025, 3, 1), "month"))
        t = P.find_dates([(5, "The Sustainability Report of Stadler Rail AG covers the reporting period from 1 January 2024 "
                               "to 31 December 2024 and was published on 19 March 2025.")], end, None)
        self.assertEqual(t[:3], (2, D(2025, 3, 19), "day"))

    def test_dates_of_other_documents_are_not_the_publication(self):
        # 24 Oct 2026 dry run: a delegated act, a press release, a bond notice, by-laws
        end = D(2024, 12, 31)
        pages = [(15, "The Omnibus I package changes are in a Delegated Act, the final version of which was published on January 8, 2025."),
                 (93, "According to the press release published March 13th, 2025, no evidence has been found."),
                 (44, "63rd Corporate Bonds (Green Bond) Publication date: May 29, 2025 Total issuance amount"),
                 (107, "On February 27, 2025, the Board approved and adopted amendments to the Company's Restated By-Laws.")]
        pages.append((4, "For example, the prior 2024 Economic Impact Report, which was published April 2025, presented data."))
        pages += [(i, "Text") for i in range(1, 5)] + [(i, "Text") for i in range(200, 204)]
        self.assertIsNone(P.find_dates(pages, end, None))

    def test_board_approval(self):
        end = D(2025, 3, 31)
        t = P.find_dates([(140, "The financial statements were authorised for issue by the Board of Directors on 27 May 2025 and signed")], end, None)
        self.assertEqual(t[:3], (3, D(2025, 5, 27), "day"))
        t = P.find_dates([(80, "On 26 February 2025, the Board of Directors approved the annual report.")], D(2024, 12, 31), None)
        self.assertEqual(t[:3], (3, D(2025, 2, 26), "day"))
        t = P.find_dates([(197, "The Standalone Financial Statements have been approved by the Board of Directors in its meeting held on 11th May, 2025.")], D(2025, 3, 31), None)
        self.assertEqual(t[:3], (3, D(2025, 5, 11), "day"))

    def test_publication_beats_approval_and_implausible_dates_are_ignored(self):
        end = D(2024, 12, 31)
        pages = [(3, "The financial statements were approved by the Board on 20 February 2025."), (2, "Publication date: 10 March 2025")]
        self.assertEqual(P.find_dates(pages, end, None)[:2], (2, D(2025, 3, 10)))
        # a date inside the period, or after the robot saw the file, is not the publication
        self.assertIsNone(P.find_dates([(1, "Published on 1 June 2024")], end, None))
        self.assertIsNone(P.find_dates([(1, "Published on 1 June 2025")], end, D(2025, 4, 1)))

    def test_period_end(self):
        self.assertEqual(P.period_end_date("2025-03", 2025), D(2025, 3, 31))
        self.assertEqual(P.period_end_date("2024-12", 2024), D(2024, 12, 31))
        self.assertEqual(P.period_end_date("", 2024), D(2024, 3, 31))     # only the year: earliest realistic end


    def test_a_sentence_about_the_earlier_edition_is_not_this_reports_date(self):
        # Bank Mandiri, 2025 Sustainability Report (26 Oct 2026): dated March 2025 from its predecessor
        text = ("Reporting Period This report presents information on the Company's operations across Indonesia for the "
                "period from 1 January to 31 December 2025 and serves as a continuation of the 2024 Sustainability Report, "
                "which was published in March 2025.")
        self.assertIsNone(P.find_dates([(39, text)], D(2025, 12, 31), None, 2025))
        # the year rule alone, with only the year known
        self.assertIsNone(P.find_dates([(39, "The 2024 Sustainability Report was published in June 2025.")],
                                       P.period_end_date("", 2025), None, 2025))
        # the subject rule alone, even when the period end is unknown
        self.assertIsNone(P.find_dates([(39, "The 2024 Sustainability Report was published in June 2025.")], None, None, 2025))
        # this edition, named with its own year, still counts
        t = P.find_dates([(2, "The 2025 Sustainability Report was published on 12 March 2026.")], D(2025, 12, 31), None, 2025)
        self.assertEqual(t[:2], (2, D(2026, 3, 12)))
        # a 2025 report cannot be published in March 2025 when only its year is known
        self.assertIsNone(P.find_dates([(1, "This report was published in March 2025.")], P.period_end_date(None, 2025), None, 2025))


class Decide(unittest.TestCase):
    def test_regulator_filing_first(self):
        f = {"Reviewer notes": "Added from the SEC regulator database (filed 2025-02-21). ", "period_end": "2024-12"}
        self.assertEqual(P.decide(f, [(1, "Published on 1 March 2025")], None)[:3], (1, D(2025, 2, 21), "day"))

    def test_first_seen_only_after_the_initial_collection(self):
        self.assertEqual(P.decide({"period_end": "2026-03"}, [], D(2026, 8, 10))[:2], (4, D(2026, 8, 10)))
        self.assertIsNone(P.decide({"period_end": "2025-12"}, [], D(2026, 3, 10)))      # initial collection
        self.assertIsNone(P.decide({"period_end": "2024-12"}, [], D(2026, 6, 20)))


if __name__ == "__main__":
    unittest.main()
